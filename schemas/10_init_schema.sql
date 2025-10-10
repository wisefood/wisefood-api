-- Create the initial schema for the database
CREATE SCHEMA IF NOT EXISTS wisefood;


-- Create Enums

-- Create Enums
DO $$
BEGIN 
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'age_groups') THEN
        CREATE TYPE age_groups AS ENUM ('child', 'teen', 'adult', 'senior', 'young_adult', 'middle_aged', 'baby');
    END IF;
END $$;

DO $$ 
BEGIN 
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'dietary_groups') THEN
        CREATE TYPE dietary_groups AS ENUM (
            'omnivore', 'vegetarian', 'lacto_vegetarian', 'ovo_vegetarian',
            'lacto_ovo_vegetarian', 'pescatarian', 'vegan', 'raw_vegan',
            'plant_based', 'flexitarian', 'halal', 'kosher', 'jain',
            'buddhist_vegetarian', 'gluten_free', 'nut_free', 'peanut_free',
            'dairy_free', 'egg_free', 'soy_free', 'shellfish_free', 'fish_free',
            'sesame_free', 'low_carb', 'low_fat', 'low_sodium', 'sugar_free',
            'no_added_sugar', 'high_protein', 'high_fiber', 'low_cholesterol',
            'low_calorie', 'keto', 'paleo', 'whole30', 'mediterranean',
            'diabetic_friendly'
        );
    END IF;
END $$;

-- Household & User Profiles
CREATE TABLE IF NOT EXISTS wisefood.household (
    id varchar(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    region VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    owner_id VARCHAR(100) NOT NULL REFERENCES keycloak.user_entity(id) ON DELETE SET NULL ON UPDATE CASCADE,
    metadata JSONB DEFAULT '{}',
    FOREIGN KEY (owner_id) REFERENCES keycloak.user_entity(id) ON DELETE SET NULL ON UPDATE CASCADE
);


CREATE TABLE IF NOT EXISTS wisefood.household_member (
    id varchar(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    image_url TEXT,
    age_group age_groups NOT NULL,
    household_id VARCHAR(100) NOT NULL,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (household_id) REFERENCES wisefood.household(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS wisefood.household_member_profile (
    id varchar(64) PRIMARY KEY,
    household_member_id VARCHAR(100) NOT NULL REFERENCES wisefood.household_member(id) ON DELETE CASCADE ON UPDATE CASCADE,
    nutritional_preferences JSONB DEFAULT '{}',
    dietary_groups dietary_groups[] DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (household_member_id) REFERENCES wisefood.household_member(id) ON DELETE CASCADE ON UPDATE CASCADE,
    UNIQUE(household_member_id)
);